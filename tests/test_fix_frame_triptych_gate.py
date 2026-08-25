"""三帧联排硬性审查门禁：[K-1] ⇄ [修后 K] ⇄ [K+1]。

这道门禁存在的理由，与节拍层 `autofix_beats` 的「不许变坏」闸门是同一条：修复流程
此前是**开环**的。`_reverify_frame_issues` 只复核「原来那几条问题解决没有」，从不问
「有没有修出新问题」——一次修复完全可以在消灭 A 问题的同时把 K-1→K 的透视撕开，
而整条链路一声不吭地报「✅ 均已消失」。帧层的这台永动机比节拍层贵得多：一次修复
是 4 选 1 重渲。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
import frame_continuity as fc
import pipeline_orchestrator as po
import candidate_selection_pipeline as csp


def _seam(status):
    """一条读得出档位的缝。"""
    return {'status': status, 'rank': fc.seam_rank({'status': status}),
            'reason': status, 'metrics': {}}


class TestCompareTriptych(unittest.TestCase):
    """两条缝的比对逻辑本身——纯函数，不碰磁盘也不碰模型。"""

    def test_a_seam_dropping_a_grade_is_a_regression(self):
        out = fc.compare_triptych({'left': _seam('passed'), 'right': _seam('passed')},
                                  {'left': _seam('failed'), 'right': _seam('passed')})
        self.assertEqual(out['verdict'], 'regressed')
        self.assertEqual(out['regressed'], ['left'])
        self.assertTrue(out['seams']['left']['regressed'])
        self.assertFalse(out['seams']['right']['regressed'])

    def test_the_downstream_seam_is_watched_too(self):
        """修 K 修裂 K→K+1 是这道门禁存在的另一半理由：只看上游会漏掉后向断层。"""
        out = fc.compare_triptych({'left': _seam('warned'), 'right': _seam('passed')},
                                  {'left': _seam('warned'), 'right': _seam('warned')})
        self.assertEqual(out['regressed'], ['right'])

    def test_an_improvement_or_a_tie_is_not_a_regression(self):
        out = fc.compare_triptych({'left': _seam('failed'), 'right': _seam('warned')},
                                  {'left': _seam('passed'), 'right': _seam('warned')})
        self.assertEqual(out['verdict'], 'ok')
        self.assertEqual(out['regressed'], [])

    def test_a_seam_without_a_baseline_is_never_judged(self):
        """修前读不出档位（缺图 / 低纹理 / 档位关闭）的缝，修后再差也不能判死一次重渲。

        这是与 analyze_frame「两个独立硬信号才否决」同源的克制：门禁不该比它更神经质，
        误判一次的代价是把一次真修好的 4 选 1 重渲丢掉。
        """
        out = fc.compare_triptych({'left': None, 'right': None},
                                  {'left': _seam('failed'), 'right': _seam('failed')})
        self.assertEqual(out['verdict'], 'unjudged')
        self.assertEqual(out['regressed'], [])
        self.assertEqual(out['judged_seams'], 0)

    def test_a_skipped_reading_carries_no_rank(self):
        skipped = {'status': 'skipped_transition', 'rank': None, 'reason': 'x', 'metrics': None}
        out = fc.compare_triptych({'left': skipped}, {'left': _seam('failed')})
        self.assertEqual(out['verdict'], 'unjudged')


class _FixFrameCase(unittest.TestCase):
    """与 test_fix_frame_cascade_downstream 同构的离线夹具。"""

    TITLE = 'test_triptych_gate_proj'
    PROMPT_BLOCK = (
        "图片 1: a raw unfinished concrete room\n"
        "视频 1: install timber studs\n"
        "图片 2: timber framing studs installed on the left wall\n"
        "视频 2: attach drywall boards\n"
        "图片 3: white drywall boards fully enclosing the room\n"
        "视频 3: install wooden floor\n"
        "图片 4: wooden floor installed in the room\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.project_dir = os.path.join(self.tmp, self.TITLE)
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        for seq in (1, 2, 3, 4):
            with open(os.path.join(self.frames_dir, f'img_{seq:03d}.webp'), 'wb') as f:
                f.write(b'fake_frame_bytes')
        server_common.write_manifest(self.project_dir, {'title': self.TITLE, 'frames': [
            {'sequence': 1, 'file': 'frames/img_001.webp', 'quality_gate': 'sequence_reviewed_pass'},
            {'sequence': 2, 'file': 'frames/img_002.webp', 'quality_gate': 'sequence_review_flagged',
             'vlm_qa_reason': '左墙龙骨缺失'},
            {'sequence': 3, 'file': 'frames/img_003.webp', 'quality_gate': 'sequence_reviewed_pass'},
            {'sequence': 4, 'file': 'frames/img_004.webp', 'quality_gate': 'sequence_reviewed_pass'},
        ]})

    def tearDown(self):
        server_common.OUTPUT_ROOT = self._orig_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTriptychGateInFixFrame(_FixFrameCase):

    def test_a_fix_that_tears_a_seam_is_rolled_back_automatically(self):
        """门禁判恶化 → 自动退回上一版，且明说自己回滚了。

        「宁可不修，也不把链改坏」——与 autofix_beats 的纪律 1 同一条。
        """
        readings = [
            {'left': _seam('passed'), 'right': _seam('passed')},   # 修前
            {'left': _seam('failed'), 'right': _seam('passed')},   # 修后：上游缝裂了
        ]
        with patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_measure_fix_triptych', side_effect=readings), \
             patch.object(po, '_reverify_frame_issues') as reverify, \
             patch.object(po, 'undo_frame_fix',
                          return_value={'prompt_block': self.PROMPT_BLOCK, 'frame': {},
                                        'at': 'x'}) as undo:
            out = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        undo.assert_called_once()
        self.assertEqual(undo.call_args[0][1], 2)
        self.assertTrue(out['rolled_back'])
        self.assertFalse(out['undoable'])
        self.assertEqual(out['triptych']['regressed'], ['left'])
        self.assertEqual(out['prompt_block'], self.PROMPT_BLOCK,
                         '回滚之后交出去的必须是退回后的提示词，不是改写后的')
        reverify.assert_not_called()

    def test_a_clean_fix_passes_the_gate_and_keeps_going(self):
        readings = [
            {'left': _seam('warned'), 'right': _seam('passed')},
            {'left': _seam('passed'), 'right': _seam('passed')},
        ]
        with patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_measure_fix_triptych', side_effect=readings), \
             patch.object(po, '_reverify_frame_issues',
                          return_value={'resolved': ['左墙龙骨缺失'], 'remaining': []}), \
             patch.object(po, 'undo_frame_fix') as undo:
            out = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        undo.assert_not_called()
        self.assertFalse(out['rolled_back'])
        self.assertTrue(out['undoable'])
        self.assertEqual(out['triptych']['verdict'], 'ok')

    def test_a_cascade_fix_reports_but_never_auto_rolls_back(self):
        """Level 3 连带重渲不自动回滚：下游已按新的 K 重渲过，而快照里只有 K 这一帧。

        单独退回 K 会留下一条修了一半的链——上游旧图、下游新血统，比不回滚更坏。
        整链快照与整链回滚是另一件事；这里只把结论大声报出来。
        """
        readings = [
            {'left': _seam('passed'), 'right': None},
            {'left': _seam('failed'), 'right': None},
        ]
        events = []
        with patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_measure_fix_triptych', side_effect=readings), \
             patch.object(po, '_reverify_frame_issues',
                          return_value={'resolved': [], 'remaining': []}), \
             patch.object(po, 'undo_frame_fix') as undo:
            out = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2,
                                     on_progress=lambda s, d: events.append((s, d)),
                                     cascade_downstream=True)

        undo.assert_not_called()
        self.assertFalse(out['rolled_back'])
        self.assertEqual(out['triptych']['verdict'], 'regressed')
        self.assertFalse(out['triptych']['auto_rollback_available'])
        gate_events = [d for s, d in events if s == 'frame_issue_triptych_gate']
        self.assertTrue(gate_events, '连带重渲判恶化时必须报出来，不能静默')
        self.assertIn('人工', gate_events[-1]['message'])


class TestMeasureFixTriptych(_FixFrameCase):
    """两条缝各自的取数口径。"""

    def _images_videos(self):
        from prompt_pipeline import _parse_prompt_slots
        return _parse_prompt_slots(self.PROMPT_BLOCK)

    def test_each_seam_reads_the_candidate_frames_own_prompt(self):
        """changed_grid_cells 圈的是候选帧自己申报的差量区，取错就把该变的算成漂移。"""
        images, videos = self._images_videos()
        seen = []

        def _fake_measure(ref, cand, *, prompt='', beat=None, mode='balanced'):
            seen.append((os.path.basename(ref or ''), os.path.basename(cand or ''), prompt))
            return _seam('passed')

        with patch.object(fc, 'measure_seam', side_effect=_fake_measure):
            po._measure_fix_triptych({}, self.TITLE, images, videos, 2)

        self.assertEqual(len(seen), 2)
        left, right = seen
        self.assertEqual((left[0], left[1]), ('img_001.webp', 'img_002.webp'))
        self.assertIn('timber framing studs', left[2])
        self.assertEqual((right[0], right[1]), ('img_002.webp', 'img_003.webp'))
        self.assertIn('drywall boards fully enclosing', right[2])

    def test_the_downstream_seam_is_dropped_when_it_is_about_to_be_rerendered(self):
        images, videos = self._images_videos()
        with patch.object(fc, 'measure_seam', return_value=_seam('passed')):
            seams = po._measure_fix_triptych({}, self.TITLE, images, videos, 2,
                                             include_right=False)
        self.assertIsNotNone(seams['left'])
        self.assertIsNone(seams['right'])

    def test_the_first_frame_has_no_upstream_seam(self):
        images, videos = self._images_videos()
        with patch.object(fc, 'measure_seam', return_value=_seam('passed')):
            seams = po._measure_fix_triptych({}, self.TITLE, images, videos, 1)
        self.assertIsNone(seams['left'], '首帧没有可继承的上游')
        self.assertIsNotNone(seams['right'])

    def test_a_camera_family_crossing_is_not_a_seam(self):
        """过门 / 换机位族的两张图本来就不该像，拿连贯性判它等于必然误判。"""
        block = self.PROMPT_BLOCK.replace("视频 1: install timber studs",
                                          "视频 1 [BRIDGE]: cross into the next room")
        from prompt_pipeline import _parse_prompt_slots
        images, videos = _parse_prompt_slots(block)
        with patch.object(fc, 'measure_seam', return_value=_seam('passed')):
            seams = po._measure_fix_triptych({}, self.TITLE, images, videos, 2)
        self.assertIsNone(seams['left'])

    def test_the_gate_respects_the_continuity_mode_switch(self):
        with patch.object(fc, 'continuity_mode', return_value='off'), \
             patch.object(fc, 'measure_seam') as measure:
            images, videos = self._images_videos()
            seams = po._measure_fix_triptych({}, self.TITLE, images, videos, 2)
        measure.assert_not_called()
        self.assertEqual(seams, {'left': None, 'right': None})


class TestFrontendReportsTheRollback(unittest.TestCase):
    """前端不得把一次回滚报成「✅ 修复完成」。

    回滚时后端返回的 `reverify` 是 None，`remaining` 为空——而 fixFrameIssue 的成功分支
    恰恰是「没有 remaining 就报成功」。两者撞在一起，用户会看到一个绿勾，而画面根本没换、
    原来那几条问题一条都还在。守顺序，不守措辞。
    """

    def test_the_rollback_branch_short_circuits_before_the_success_branch(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, 'js', 'api_client.js'), encoding='utf-8').read()
        rollback = src.find('watch.result.rolled_back')
        success = src.find("修复完成`, 'ok')")
        self.assertNotEqual(rollback, -1, 'fixFrameIssue 必须先拦回滚那一路')
        self.assertNotEqual(success, -1)
        self.assertLess(rollback, success,
                        '回滚分支必须排在「修复完成」之前并直接 return')

    def test_the_gate_event_is_rendered(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, 'js', 'api_client.js'), encoding='utf-8').read()
        self.assertIn("frame_issue_triptych_gate", src,
                      '门禁事件没人接的话，回滚在界面上是静默发生的')


if __name__ == '__main__':
    unittest.main()
