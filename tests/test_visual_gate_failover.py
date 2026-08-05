"""视觉判定的环境异常出口。

2026-08-05：生成期一致性审查（逐帧 VLM 质检、跨帧漂移复查、锚帧验收门）已整体移除，
本文件里针对那三个 judge 的 fail-open/fail-closed 用例随之删除。留下的是仍在生产路径
上的两条：判定调用的统一重试通道（手动一致性审查、视频施工过程复审共用），以及视频
锚点 MAD 校验的 strict 行为。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline
from video_generator import verify_video_anchors


class TestJudgeCallRetryChannel(unittest.TestCase):
    """质检判定调用必须走统一重试通道（_execute_request_with_retry）：瞬时抖动
    快速重试一次，而不是一次 90s 裸超时就 fail-open 成 auto_approved_degraded；
    且每次失败会经线程本地 sink 即时广播进帧序列实时动态流。"""

    def test_multimodal_chat_uses_retry_channel_with_quick_retry(self):
        chat_json = json.dumps({'choices': [{'message': {'content': 'PASS'}}]}).encode('utf-8')
        with tempfile.TemporaryDirectory() as td:
            img_a = os.path.join(td, 'a.webp')
            with open(img_a, 'wb') as f:
                f.write(b'x')
            with patch.object(prompt_pipeline, '_execute_request_with_retry',
                              return_value=chat_json) as mock_exec:
                out = prompt_pipeline._multimodal_chat({}, 'system', 'user', [img_a])
        self.assertEqual(out, 'PASS')
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
