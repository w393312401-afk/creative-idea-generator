import json
import os
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline
from prompt_pipeline import (
    is_skipped_verdict,
    run_vlm_qa_check,
    check_landmark_drift,
    check_anchor_frame_compliance,
)
from video_generator import verify_video_anchors


class TestJudgeApiFailover(unittest.TestCase):
    """视觉 judge 的 API 异常出口：默认 fail-open（放行 + Skipped 留痕 + 计入
    _skipped_checks），strictGates 开启时 fail-closed（按判定失败处理）。
    此前三个 judge 在 API 异常时一律静默返回 (True, 'Skipped...')，判定服务宕机
    会让整套视觉门失效且 manifest 仍记 auto_approved。"""

    def _boom(self, *args, **kwargs):
        raise RuntimeError('vlm endpoint down')

    def test_fail_open_by_default_and_counts_skipped(self):
        config = {}
        with patch.object(prompt_pipeline, '_multimodal_chat', side_effect=self._boom):
            passed, reason = run_vlm_qa_check(config, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))
        self.assertEqual(config['_skipped_checks'], 1)

    def test_all_three_judges_fail_closed_under_strict_gates(self):
        config = {'strictGates': True}
        with patch.object(prompt_pipeline, '_multimodal_chat', side_effect=self._boom):
            vlm = run_vlm_qa_check(config, 'a.webp', 'b.webp', 'video prompt')
            drift = check_landmark_drift(config, 'img1.webp', 'imgN.webp')
            anchor = check_anchor_frame_compliance(config, 'img1.webp', 'prompt', {}, {})
        for passed, reason in (vlm, drift, anchor):
            self.assertFalse(passed)
            self.assertFalse(is_skipped_verdict(reason))
        self.assertEqual(config['_skipped_checks'], 3)

    def test_real_pass_is_not_a_skipped_verdict(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value='PASS'):
            passed, reason = run_vlm_qa_check({}, 'a.webp', 'b.webp', 'video prompt')
        self.assertTrue(passed)
        self.assertFalse(is_skipped_verdict(reason))

    def test_real_fail_still_fails_without_strict(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value='FAIL: 画面跳变'):
            passed, reason = run_vlm_qa_check({}, 'a.webp', 'b.webp', 'video prompt')
        self.assertFalse(passed)
        self.assertIn('跳变', reason)

    def test_combined_qa_keeps_skip_marker_when_adjacent_check_was_skipped(self):
        """seq>2 时邻帧动作校验被跳过（judge 异常放行）而漂移检查真实 PASS：
        合并结论必须保留 Skipped 标记，否则未经动作校验的帧会被记成 auto_approved。"""
        with tempfile.TemporaryDirectory() as td:
            img1 = os.path.join(td, 'img_001.webp')
            target = os.path.join(td, 'img_003.webp')
            for p in (img1, target):
                with open(p, 'wb') as f:
                    f.write(b'x')
            with patch.object(prompt_pipeline, 'run_vlm_qa_check',
                              return_value=(True, 'Skipped (API Error: boom)')), \
                 patch.object(prompt_pipeline, 'check_landmark_drift',
                              return_value=(True, 'PASS')):
                passed, reason = prompt_pipeline.run_frame_qa_check(
                    {}, img1, 'prev.webp', target, 'video prompt', seq=3)
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))


class TestJudgeCallRetryChannel(unittest.TestCase):
    """质检判定调用必须走统一重试通道（_execute_request_with_retry）：瞬时抖动
    快速重试一次，而不是一次 90s 裸超时就 fail-open 成 auto_approved_degraded；
    且每次失败会经线程本地 sink 即时广播进帧序列实时动态流。"""

    def test_multimodal_chat_uses_retry_channel_with_quick_retry(self):
        chat_json = json.dumps({'choices': [{'message': {'content': 'PASS'}}]}).encode('utf-8')
        with tempfile.TemporaryDirectory() as td:
            img_a = os.path.join(td, 'a.webp')
            img_b = os.path.join(td, 'b.webp')
            for p in (img_a, img_b):
                with open(p, 'wb') as f:
                    f.write(b'x')
            with patch.object(prompt_pipeline, '_execute_request_with_retry',
                              return_value=chat_json) as mock_exec:
                passed, reason = run_vlm_qa_check({}, img_a, img_b, 'video prompt')
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        mock_exec.assert_called_once()
        self.assertEqual(mock_exec.call_args.kwargs.get('max_attempts'), 2)


class TestVerifyVideoAnchorsStrict(unittest.TestCase):
    """视频锚点 MAD 校验的环境异常处理：默认 skipped 放行，strict 时按失败处理。
    锚点图缺失（无从比对）时无论 strict 与否都放行——那是上游帧完整性门的职责。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.anchor = os.path.join(self.tmp, 'anchor.webp')
        with open(self.anchor, 'wb') as f:
            f.write(b'not really an image')
        self.missing_video = os.path.join(self.tmp, 'no_such_video.mp4')

    def test_extract_failure_skips_by_default(self):
        ok, reason = verify_video_anchors(self.missing_video, self.anchor, None)
        self.assertTrue(ok)
        self.assertTrue(reason.startswith('skipped:'))

    def test_extract_failure_fails_under_strict(self):
        ok, reason = verify_video_anchors(self.missing_video, self.anchor, None, strict=True)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('strict:'))

    def test_no_anchor_passes_by_default(self):
        ok, reason = verify_video_anchors(self.missing_video, None, None)
        self.assertTrue(ok)
        self.assertEqual(reason, 'skipped:no_anchor')

    def test_no_anchor_fails_under_strict(self):
        """锚点图缺失（被清理/移动）也是环境退化：严格模式下不得静默放行。"""
        ok, reason = verify_video_anchors(self.missing_video, None, None, strict=True)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('strict:'))


if __name__ == '__main__':
    unittest.main()
