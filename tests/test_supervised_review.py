"""关键点监修模式测试。

supervisedMode 开启后，流水线在判定器最弱的关键点异步暂停等人工确认：
首帧过门后、镜头族交接锚点帧渲染后（_render_frames_with_checkpoints 里族锚单独成段）、
降级帧汇总处。决策经任务带内通道回传（on_progress('review_poll') 探测，
与 cancel_check 同款）；超时自动采用——监修是加严不是门禁。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pipeline_orchestrator
from prompt_pipeline import _format_prompt_block
from pipeline_orchestrator import (
    _await_frame_review,
    _render_frames_with_checkpoints,
    _supervised_degraded_summary,
    _supervised_mode,
    _review_timeout,
)


class _FakeProgress:
    """可编程的 on_progress：记录事件流，按队列回答 review_poll。"""

    def __init__(self, decisions=None, cancelled=False):
        self.events = []
        self.decisions = list(decisions or [])
        self.cancelled = cancelled

    def __call__(self, stage, details):
        if stage == 'cancel_check':
            return self.cancelled
        if stage == 'review_poll':
            return self.decisions.pop(0) if self.decisions else None
        self.events.append((stage, details))
        return None

    def stages(self):
        return [s for s, _ in self.events]


def _instant_sleep():
    return patch.object(pipeline_orchestrator.time, 'sleep', lambda s: None)


class TestModeAndTimeoutHelpers(unittest.TestCase):
    def test_supervised_mode_flag(self):
        self.assertFalse(_supervised_mode({}))
        self.assertFalse(_supervised_mode(None))
        self.assertTrue(_supervised_mode({'supervisedMode': True}))

    def test_review_timeout_clamps(self):
        self.assertEqual(_review_timeout({}), 600)
        self.assertEqual(_review_timeout({'reviewTimeoutSeconds': 5}), 30)   # 下限钳位
        self.assertEqual(_review_timeout({'reviewTimeoutSeconds': 'x'}), 600)
        self.assertEqual(_review_timeout({'reviewTimeoutSeconds': 1200}), 1200)


class TestAwaitFrameReview(unittest.TestCase):
    def test_adopt_decision(self):
        cb = _FakeProgress(decisions=[None, 'adopt'])
        with _instant_sleep():
            decision = _await_frame_review({}, 'proj', 3, 'img.webp', '测试点', cb)
        self.assertEqual(decision, 'adopt')
        self.assertEqual(cb.stages(), ['review_pause', 'review_resume'])
        pause = cb.events[0][1]
        self.assertEqual(pause['sequence'], 3)
        self.assertIn('监修暂停', pause['message'])

    def test_rerender_decision(self):
        cb = _FakeProgress(decisions=['rerender'])
        with _instant_sleep():
            self.assertEqual(_await_frame_review({}, 'proj', 3, 'img.webp', 'ctx', cb), 'rerender')

    def test_timeout_auto_adopts(self):
        cb = _FakeProgress()  # 永远没有决策
        with _instant_sleep(), patch.object(pipeline_orchestrator, '_review_timeout', lambda c: 0):
            decision = _await_frame_review({}, 'proj', 1, 'img.webp', 'ctx', cb)
        self.assertEqual(decision, 'adopt')
        resume = cb.events[-1][1]
        self.assertTrue(resume.get('timeout'))

    def test_cancel_raises_connection_error(self):
        cb = _FakeProgress(cancelled=True)
        with _instant_sleep():
            with self.assertRaises(ConnectionError):
                _await_frame_review({}, 'proj', 1, 'img.webp', 'ctx', cb)

    def test_no_progress_cb_auto_adopts(self):
        self.assertEqual(_await_frame_review({}, 'proj', 1, 'img.webp', 'ctx', None), 'adopt')


class _TmpProjectCase(unittest.TestCase):
    TITLE = 'proj'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.frames_dir)
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'title': self.TITLE, 'frames': []}, f)
        self._patch = patch.object(pipeline_orchestrator, '_get_project_dir',
                                   lambda title: self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch_frames(self, seqs):
        for i in seqs:
            with open(os.path.join(self.frames_dir, f'img_{i:03d}.webp'), 'wb') as f:
                f.write(b'x')


class TestSupervisedSegmentation(_TmpProjectCase):
    """监修模式的分段变化：族锚帧（首帧/桥接交接帧）单独成段 + 渲染后审阅；
    重渲决策触发单帧重渲后再次审阅。"""

    def _run(self, n_images, bridge_at=None, reviews=None, config=None):
        images = {i: {'body': f'frame {i}.', 'meta': ''} for i in range(1, n_images + 1)}
        videos = {i: {'body': f'v{i}', 'meta': 'BRIDGE' if i == bridge_at else ''}
                  for i in range(1, n_images)}
        block = _format_prompt_block(images, videos)
        calls = []
        review_calls = []
        reviews = list(reviews or [])

        def fake_generate(cfg, title, blk, on_progress=None, target_sequences=None):
            calls.append(list(target_sequences) if target_sequences else None)
            for s in (target_sequences or []):
                self._touch_frames([s])

        def fake_review(cfg, title, seq, image_path, context, on_progress):
            review_calls.append((seq, context))
            return reviews.pop(0) if reviews else 'adopt'

        cfg = config if config is not None else {'supervisedMode': True}
        with patch.object(pipeline_orchestrator, 'generate_frame_sequence', fake_generate), \
             patch.object(pipeline_orchestrator, '_await_frame_review', fake_review), \
             patch.object(pipeline_orchestrator, '_checkpoint_reality_sync',
                          lambda *a, **k: False):
            _render_frames_with_checkpoints(cfg, self.TITLE, block, self.tmp)
        return calls, review_calls

    def test_anchor_frames_get_solo_segment_and_review(self):
        calls, reviews = self._run(12, bridge_at=6)
        # 族1 = IMG1-6（锚1单独成段），族2 = IMG7-12（锚7单独成段）
        self.assertEqual(calls, [[1], [2, 3, 4, 5, 6], [7], [8, 9, 10, 11, 12]])
        self.assertEqual([r[0] for r in reviews], [1, 7])
        self.assertIn('首帧', reviews[0][1])
        self.assertIn('交接', reviews[1][1])

    def test_rerender_decision_rerenders_anchor_then_reviews_again(self):
        calls, reviews = self._run(8, reviews=['rerender', 'adopt'])
        # [1] 渲染 → 审阅=重渲 → [1] 再渲 → 审阅=采用 → 继续
        self.assertEqual(calls[0], [1])
        self.assertEqual(calls[1], [1])
        self.assertEqual([r[0] for r in reviews], [1, 1])

    def test_small_chain_still_pauses_when_supervised(self):
        # 全自动模式下 4 帧走单次全量调用；监修模式必须仍然分段以安放暂停点
        calls, reviews = self._run(4)
        self.assertEqual(calls, [[1], [2, 3, 4]])
        self.assertEqual([r[0] for r in reviews], [1])

    def test_unsupervised_defaults_to_bridge_anchor_review(self):
        # 2026-07-15 起全自动模式（config={}）也默认监修桥接/换族锚点
        # （bridgeAnchorReview 默认开）：换族锚 IMG7 单独成段并审阅；
        # 首帧监修仍只归 supervisedMode 管，不打扰。
        calls, reviews = self._run(12, bridge_at=6, config={})
        self.assertEqual(calls, [[1, 2, 3, 4, 5], [6], [7], [8, 9, 10, 11, 12]])
        self.assertEqual([r[0] for r in reviews], [7])
        self.assertIn('交接', reviews[0][1])

    def test_bridge_review_disabled_keeps_original_segmentation(self):
        calls, reviews = self._run(12, bridge_at=6, config={'bridgeAnchorReview': False})
        self.assertEqual(calls, [[1, 2, 3, 4, 5], [6], [7, 8, 9, 10, 11], [12]])
        self.assertEqual(reviews, [])

    def test_existing_anchor_not_rereviewed(self):
        # 断点续传：首帧已在盘上（例如 staged 路径已过门），不重复打扰——
        # 不再单列锚点段（按常规 [1..5] 分段），目标里也剔除已有帧
        self._touch_frames([1])
        calls, reviews = self._run(8)
        self.assertEqual(calls[0], [2, 3, 4, 5])
        self.assertEqual(reviews, [])


class TestDegradedSummaryPause(_TmpProjectCase):
    def _write_manifest(self, gates):
        frames = [{'sequence': i + 1, 'quality_gate': g} for i, g in enumerate(gates)]
        with open(os.path.join(self.tmp, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'title': self.TITLE, 'frames': frames}, f)

    def test_pauses_when_degraded_frames_exist(self):
        self._write_manifest(['auto_approved', 'sequence_review_flagged', 'auto_approved_degraded'])
        pauses = []
        with patch.object(pipeline_orchestrator, '_await_frame_review',
                          lambda *a, **k: pauses.append(a) or 'adopt'):
            _supervised_degraded_summary({'supervisedMode': True}, self.TITLE, self.tmp)
        self.assertEqual(len(pauses), 1)
        context = pauses[0][4]
        self.assertIn('IMG 002', context)
        self.assertIn('IMG 003', context)

    def test_no_pause_when_all_clean_or_unsupervised(self):
        self._write_manifest(['auto_approved', 'auto_approved'])

        def _boom(*a, **k):
            raise AssertionError('干净的链不应暂停')
        with patch.object(pipeline_orchestrator, '_await_frame_review', _boom):
            _supervised_degraded_summary({'supervisedMode': True}, self.TITLE, self.tmp)
        self._write_manifest(['vlm_qa_failed'])
        with patch.object(pipeline_orchestrator, '_await_frame_review', _boom):
            _supervised_degraded_summary({}, self.TITLE, self.tmp)


if __name__ == '__main__':
    unittest.main()
