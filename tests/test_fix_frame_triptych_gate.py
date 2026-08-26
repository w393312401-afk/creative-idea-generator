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


def _seam(status, hard=('camera',)):
    """一条读得出档位的缝。

    `hard` ＝这条读数带的硬漂移信号。warned 有两种来源（一个硬信号 vs 低纹理/差量区
    推进不足这类软信号），门禁只认前者，见 fc.effective_seam_rank。"""
    return {'status': status, 'rank': fc.seam_rank({'status': status}),
            'reason': status, 'metrics': {}, 'hard_votes': list(hard)}


def _soft_seam(status):
    """纯软信号造成的读数：低纹理、差量区推进不足。"""
    return {'status': status, 'rank': fc.seam_rank({'status': status}),
            'reason': status, 'metrics': {}, 'hard_votes': [],
            'warning_votes': ['low_texture']}


class TestCompareTriptych(unittest.TestCase):
    """两条缝的比对逻辑本身——纯函数，不碰磁盘也不碰模型。"""

    def test_a_seam_dropping_a_grade_is_a_regression(self):
        out = fc.compare_triptych({'left': _seam('passed'), 'right': _seam('passed')},
                                  {'left': _seam('failed'), 'right': _seam('passed')})
        self.assertEqual(out['verdict'], 'regressed')
        self.assertEqual(out['regressed'], ['left'])
        self.assertTrue(out['seams']['left']['regressed'])
        self.assertFalse(out['seams']['right']['regressed'])

    def test_the_downstream_seam_only_blocks_when_the_chain_actually_breaks(self):
        """右缝＝K → K+1，而 K+1 是从**修复前**的 K 图生图出来的：修复越有效，这条缝
        越不像。拿它当严格判据等于要求这次修复什么都别改——门禁会系统性地否掉正是它
        本该放行的那类修复。所以右缝只在跌到 failed（两个独立硬信号＝链真的断了）时
        才拦；跌到 warned 照常报出来，但不回滚。"""
        soft = fc.compare_triptych({'left': _seam('warned'), 'right': _seam('passed')},
                                   {'left': _seam('warned'), 'right': _seam('warned')})
        self.assertEqual(soft['regressed'], [])
        self.assertEqual(soft['verdict'], 'ok')
        self.assertEqual(soft['worsened'], ['right'], '不拦，但必须如实报出来')
        self.assertIn('不构成拦截', fc.describe_triptych(soft))

        hard = fc.compare_triptych({'left': _seam('warned'), 'right': _seam('passed')},
                                   {'left': _seam('warned'), 'right': _seam('failed')})
        self.assertEqual(hard['regressed'], ['right'])

    def test_the_upstream_seam_stays_strict(self):
        """左缝＝K-1 → K。上游是既成事实，修 K 不该动它，任何严格恶化都拦。"""
        out = fc.compare_triptych({'left': _seam('passed')}, {'left': _seam('warned')})
        self.assertEqual(out['regressed'], ['left'])

    def test_a_soft_warning_is_not_a_regression(self):
        """低纹理 / 差量区推进不足单独就能把 passed 顶成 warned，但那不是"画面被改坏"。
        analyze_frame 自己要两个独立硬信号才否决，门禁不该比它更神经质——误判一次的
        代价是把一次真修好的 4 选 1 重渲丢掉。"""
        out = fc.compare_triptych({'left': _seam('passed', hard=())},
                                  {'left': _soft_seam('warned')})
        self.assertEqual(out['regressed'], [])
        self.assertEqual(out['verdict'], 'ok')
        # 但硬信号造成的同一档跌落照拦不误
        hard = fc.compare_triptych({'left': _seam('passed', hard=())},
                                   {'left': _seam('warned')})
        self.assertEqual(hard['regressed'], ['left'])

    def test_a_reading_without_the_hard_vote_field_keeps_its_grade(self):
        """旧读数 / 外部构造的读数没有 hard_votes ＝硬软不明，保守按原档位算。"""
        old = {'status': 'warned', 'rank': 1, 'reason': 'x', 'metrics': {}}
        out = fc.compare_triptych({'left': {'status': 'passed', 'rank': 0}}, {'left': old})
        self.assertEqual(out['regressed'], ['left'])

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


class TestRejectedFixIsKept(_FixFrameCase):
    """门禁判恶化会自动回滚，而 undo_frame_fix 用完即删快照——判错一次，那次 4 选 1
    重渲的结果就**彻底**没了，用户连"其实我想要那版"都没处说。门禁是概率判定，两条缝
    的口径已经放宽过一轮，但假阳性不可能归零。所以回滚前留档，并给一个采用的出口。"""

    FIXED_BYTES = b'the fixed version'

    def _render_writes_the_fixed_frame(self, *a, **kw):
        """重渲＝覆盖写同一个帧文件。修后那一版只在这一刻存在于磁盘上。"""
        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'wb') as f:
            f.write(self.FIXED_BYTES)

    def _rolled_back(self, cascade=False):
        readings = [
            {'left': _seam('passed'), 'right': None},
            {'left': _seam('failed'), 'right': None},
        ]
        with patch.object(csp, 'run_candidate_selection_frame_sequence',
                          side_effect=self._render_writes_the_fixed_frame), \
             patch.object(po, '_measure_fix_triptych', side_effect=readings), \
             patch.object(po, '_reverify_frame_issues'):
            return po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2,
                                      cascade_downstream=cascade)

    def test_the_rejected_version_is_stashed_and_advertised(self):
        out = self._rolled_back()

        self.assertTrue(out['rolled_back'])
        self.assertTrue(out['rejected_fix']['at'])
        stash = po._rejected_fix_dir(self.project_dir, 2)
        with open(os.path.join(stash, 'img_002.webp'), 'rb') as f:
            self.assertEqual(f.read(), b'the fixed version', '留的必须是修后那一版')
        frame = po._frame_manifest_entry(self.project_dir, 2)
        self.assertTrue(frame['rejected_fix'], '记号要盖在回滚之后，否则会被条目还原冲掉')

    def test_adopting_puts_the_fixed_version_back_and_stays_undoable(self):
        rolled = self._rolled_back()
        restored_block = rolled['prompt_block']
        # 回滚之后磁盘上是修复前那版
        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'rb') as f:
            self.assertEqual(f.read(), b'fake_frame_bytes')

        out = po.adopt_rejected_fix(self.TITLE, 2, restored_block)

        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'rb') as f:
            self.assertEqual(f.read(), b'the fixed version')
        frame = po._frame_manifest_entry(self.project_dir, 2)
        self.assertEqual(frame['quality_gate'], 'pending_manual_review',
                         '这一版没经过复核，不能谎报审查通过')
        self.assertNotIn('rejected_fix', frame, '采用之后这枚记号就不成立了')
        self.assertTrue(frame['fix_backup'], '采用不是单向门：还能撤回来')
        self.assertFalse(os.path.exists(po._rejected_fix_dir(self.project_dir, 2)))
        # 撤销回到采用之前那一版
        po.undo_frame_fix(self.TITLE, 2, out['prompt_block'])
        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'rb') as f:
            self.assertEqual(f.read(), b'fake_frame_bytes')

    def test_adopting_without_a_stash_raises_instead_of_pretending(self):
        with self.assertRaises(RuntimeError):
            po.adopt_rejected_fix(self.TITLE, 2, self.PROMPT_BLOCK)

    def test_a_new_fix_supersedes_the_stashed_version(self):
        """又修了一遍，"采用上次那版"已经没有意义——留着只会让人采用到一版比当前
        还旧的画面。"""
        self._rolled_back()
        self.assertTrue(os.path.exists(po._rejected_fix_dir(self.project_dir, 2)))
        readings = [{'left': _seam('passed')}, {'left': _seam('passed')}]
        with patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_measure_fix_triptych', side_effect=readings), \
             patch.object(po, '_reverify_frame_issues',
                          return_value={'resolved': ['左墙龙骨缺失'], 'remaining': []}):
            po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)
        self.assertFalse(os.path.exists(po._rejected_fix_dir(self.project_dir, 2)))


class TestCascadeLeavesNoHalfChainUndo(_FixFrameCase):
    """三联屏门禁拒绝在连带重渲后自动回滚 K，理由是那会留下上游旧图 + 下游新血统的
    半截链。那么手动入口同样不该敞着门——此前 fix_backup 记号照盖不误，人手点一下
    「撤销修复」造出来的正是同一条半截链。"""

    def _fix(self, cascade):
        readings = [{'left': _seam('passed'), 'right': _seam('passed')},
                    {'left': _seam('passed'), 'right': _seam('passed')}]
        with patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_measure_fix_triptych', side_effect=readings), \
             patch.object(po, '_reverify_frame_issues',
                          return_value={'resolved': ['左墙龙骨缺失'], 'remaining': []}):
            return po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2,
                                      cascade_downstream=cascade)

    def test_a_single_frame_fix_stays_undoable(self):
        self._fix(cascade=False)
        self.assertTrue(po._frame_manifest_entry(self.project_dir, 2).get('fix_backup'))
        self.assertTrue(os.path.exists(po._fix_snapshot_dir(self.project_dir, 2)))

    def test_a_cascade_fix_offers_no_undo_at_all(self):
        self._fix(cascade=True)
        self.assertIsNone(po._frame_manifest_entry(self.project_dir, 2).get('fix_backup'))
        self.assertFalse(os.path.exists(po._fix_snapshot_dir(self.project_dir, 2)),
                         '连一个"退回来只对了一半"的快照都不留')


class TestMeasureFixTriptych(_FixFrameCase):
    """两条缝各自的取数口径。"""

    def _images_videos(self):
        from prompt_pipeline import _parse_prompt_slots
        return _parse_prompt_slots(self.PROMPT_BLOCK)

    def test_each_seam_reads_the_candidate_frames_own_prompt(self):
        """changed_grid_cells 圈的是候选帧自己申报的差量区，取错就把该变的算成漂移。"""
        images, videos = self._images_videos()
        seen = []

        def _fake_measure(ref, cand, *, prompt='', beat=None, mode='balanced', cells=None):
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

    def test_each_seam_carries_the_structured_beat_like_the_forward_path(self):
        """前向建链渲每一帧时是带着 spatial_beats 调 analyze_frame 的
        （frame_generator._continuity_beat），门禁必须同源取数。少了它两处走样：
        changed_grid_cells 退化成正则扫正文、扫不到就兜底成 B2（"除中心格外整张图都算
        稳定区"）；只靠 spatial_beats 声明的过场帧也认不出来。"""
        images, videos = self._images_videos()
        server_common.write_manifest(self.project_dir, dict(
            server_common.read_manifest(self.project_dir),
            spatial_beats=[{'index': 1, 'changed_grid_cells': ['A1']},
                           {'index': 2, 'changed_grid_cells': ['C3']}]))
        seen = {}

        def _fake_measure(ref, cand, *, prompt='', beat=None, mode='balanced', cells=None):
            seen[os.path.basename(cand or '')] = (beat, cells)
            return _seam('passed')

        with patch.object(fc, 'measure_seam', side_effect=_fake_measure):
            po._measure_fix_triptych({}, self.TITLE, images, videos, 2)

        # 左缝的候选帧是 IMG 002 → 第 1 拍；右缝的候选帧是 IMG 003 → 第 2 拍
        self.assertEqual(seen['img_002.webp'][0]['changed_grid_cells'], ['A1'])
        self.assertEqual(seen['img_003.webp'][0]['changed_grid_cells'], ['C3'])

    def test_a_transition_declared_only_in_the_beat_ladder_is_still_skipped(self):
        images, videos = self._images_videos()
        server_common.write_manifest(self.project_dir, dict(
            server_common.read_manifest(self.project_dir),
            spatial_beats=[{'index': 1, 'hard_cut': True}]))
        with patch.object(fc, 'measure_seam', return_value=_seam('passed')):
            seams = po._measure_fix_triptych({}, self.TITLE, images, videos, 2)
        self.assertIsNone(seams['left'], '拍梯里声明的硬切同样是一条不该判的缝')

    def test_the_second_reading_reuses_the_first_ones_change_region(self):
        """修前 / 修后必须用同一块掩膜。定向修复恰恰会改写候选帧自己的正文，重新推断出
        来的差量区与修前不同，比较的就是两个不同口径的读数——档位差异可能纯粹来自掩膜
        位移，而不是画面真变坏了。"""
        images, videos = self._images_videos()
        seen = []

        def _fake_measure(ref, cand, *, prompt='', beat=None, mode='balanced', cells=None):
            seen.append(cells)
            return dict(_seam('passed'), cells=['B1'])

        with patch.object(fc, 'measure_seam', side_effect=_fake_measure):
            before = po._measure_fix_triptych({}, self.TITLE, images, videos, 2)
            po._measure_fix_triptych({}, self.TITLE, images, videos, 2, baseline=before)

        self.assertEqual(seen[:2], [None, None], '第一次没有基线，照常自己推断')
        self.assertEqual(seen[2:], [['B1'], ['B1']], '第二次原样复用第一次算出的差量区')

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
