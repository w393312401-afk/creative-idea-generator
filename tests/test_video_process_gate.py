"""段内过程门（空心视频拦截）测试。

verify_video_anchors 只钉住首尾两端，段内内容是盲区（空心视频/幽灵施工的来源）。
新增的链路：_extract_video_mid_frames 抽中段帧 → prompt_pipeline.run_video_process_check
VLM 判定 → video_generator.check_video_process 按档位映射动作 → _BatchBridge 拒收/告警。
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline
import video_generator
from prompt_pipeline import run_video_process_check, is_skipped_verdict, is_warn_verdict
from video_generator import check_video_process, _extract_video_mid_frames, _BatchBridge


class TestRunVideoProcessCheck(unittest.TestCase):
    """VLM 判定层：PASS / PASS_WITH_WARNINGS / FAIL 三态解析，off 档短路，
    判定服务异常 fail-open（strictGates 开启时 fail-closed）。"""

    IMGS = dict(start_frame_path='s.png', mid_frame_paths=['m1.png', 'm2.png'],
                end_frame_path='e.png', video_prompt='crew installs the window')

    def test_pass(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value='PASS'):
            passed, reason = run_video_process_check({}, **self.IMGS)
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')

    def test_fail_reason_passthrough(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', return_value='FAIL: H1 空心片段'):
            passed, reason = run_video_process_check({}, **self.IMGS)
        self.assertFalse(passed)
        self.assertIn('H1', reason)

    def test_warn_normalized(self):
        with patch.object(prompt_pipeline, '_multimodal_chat',
                          return_value='PASS_WITH_WARNINGS: 中段推进略少'):
            passed, reason = run_video_process_check({}, **self.IMGS)
        self.assertTrue(passed)
        self.assertTrue(is_warn_verdict(reason))

    def test_off_level_skips_without_vlm_call(self):
        def _boom(*a, **k):
            raise AssertionError('off 档不得发 VLM 请求')
        with patch.object(prompt_pipeline, '_multimodal_chat', _boom):
            passed, reason = run_video_process_check({'qaGateLevel': 'off'}, **self.IMGS)
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))

    def test_judge_error_fails_open_by_default(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', side_effect=RuntimeError('boom')):
            passed, reason = run_video_process_check({}, **self.IMGS)
        self.assertTrue(passed)
        self.assertTrue(is_skipped_verdict(reason))

    def test_judge_error_fails_closed_with_strict_gates(self):
        with patch.object(prompt_pipeline, '_multimodal_chat', side_effect=RuntimeError('boom')):
            passed, reason = run_video_process_check({'strictGates': True}, **self.IMGS)
        self.assertFalse(passed)
        self.assertTrue(reason.startswith('FAIL'))


class TestCheckVideoProcessMapping(unittest.TestCase):
    """档位 → 动作映射：standard 拒收 / lenient 告警放行 / off 跳过；
    抽帧环境异常按 verify_video_anchors 同款语义（默认放行留痕、strict 拒收）。"""

    ARGS = ('vid.mp4', 's.png', 'e.png', 'prompt text')

    def test_off_level_short_circuits_before_extraction(self):
        def _boom(*a, **k):
            raise AssertionError('off 档不得抽帧')
        with patch.object(video_generator, '_extract_video_mid_frames', _boom):
            action, reason = check_video_process({'qaGateLevel': 'off'}, *self.ARGS)
        self.assertEqual(action, 'accept')
        self.assertTrue(reason.startswith('Skipped'))

    def test_extraction_failure_accepts_with_trace_by_default(self):
        with patch.object(video_generator, '_extract_video_mid_frames', return_value=[]):
            action, reason = check_video_process({}, *self.ARGS)
        self.assertEqual(action, 'accept')
        self.assertEqual(reason, 'skipped:mid_extract_failed')

    def test_extraction_failure_rejects_when_strict(self):
        with patch.object(video_generator, '_extract_video_mid_frames', return_value=[]):
            action, reason = check_video_process({'strictGates': True}, *self.ARGS)
        self.assertEqual(action, 'reject')
        self.assertIn('strict', reason)

    def test_vlm_fail_rejects_at_standard(self):
        with patch.object(video_generator, '_extract_video_mid_frames', return_value=['m.png']), \
             patch.object(prompt_pipeline, 'run_video_process_check',
                          return_value=(False, 'FAIL: H1 空心片段')):
            action, reason = check_video_process({'qaGateLevel': 'standard'}, *self.ARGS)
        self.assertEqual(action, 'reject')
        self.assertIn('H1', reason)

    def test_vlm_fail_warns_at_lenient(self):
        # 宽松档不拒收：重试要再烧一整段视频额度，保留成片 + 告警留痕交用户决策
        with patch.object(video_generator, '_extract_video_mid_frames', return_value=['m.png']), \
             patch.object(prompt_pipeline, 'run_video_process_check',
                          return_value=(False, 'FAIL: H1 空心片段')):
            action, reason = check_video_process({'qaGateLevel': 'lenient'}, *self.ARGS)
        self.assertEqual(action, 'warn')
        self.assertIn('H1', reason)

    def test_vlm_pass_accepts(self):
        with patch.object(video_generator, '_extract_video_mid_frames', return_value=['m.png']), \
             patch.object(prompt_pipeline, 'run_video_process_check', return_value=(True, 'PASS')):
            action, reason = check_video_process({}, *self.ARGS)
        self.assertEqual(action, 'accept')
        self.assertEqual(reason, 'PASS')


class TestBatchBridgeProcessGate(unittest.TestCase):
    """_BatchBridge 集成：reject 删片回传 'rejected'（进入批量重试轮），
    warn 发 video_warning 且成功落 manifest 带 process_check 留痕，
    不注入 process_check_fn 时行为与旧版完全一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.records = []
        self.events = []

        class Writer:
            def record(_, info):
                self.records.append(info)
        self.writer = Writer()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bridge_and_plan(self, process_check_fn):
        # start/end 锚点留空：verify_video_anchors 走 'skipped:no_anchor' 放行，
        # 使测试聚焦段内过程门本身
        plan = {'slot': 3, 'seq': 1, 'prompt': 'p',
                'dest_path': os.path.join(self.tmp, 'vid_003.mp4'),
                'start_frame': None, 'end_frame': None}
        bridge = _BatchBridge(
            pending=[{'plan': plan}], total=1, video_model='veo', writer=self.writer,
            on_progress=lambda stage, payload: self.events.append((stage, payload)),
            process_check_fn=process_check_fn)
        return bridge, plan

    def _downloaded(self):
        src = os.path.join(self.tmp, 'raw.mp4')
        with open(src, 'wb') as f:
            f.write(b'x')
        return src

    def test_reject_deletes_file_and_returns_rejected(self):
        bridge, plan = self._bridge_and_plan(lambda p: ('reject', 'FAIL: H1 空心片段'))
        ret = bridge(0, 'video_done', {'video_url': self._downloaded()})
        self.assertEqual(ret, 'rejected')
        self.assertFalse(os.path.exists(plan['dest_path']))
        self.assertEqual(self.records[0]['status'], 'failed')
        self.assertIn('段内过程检测未通过', self.records[0]['error'])
        self.assertEqual(self.events[-1][0], 'video_error')

    def test_warn_records_success_with_trace_and_emits_warning(self):
        bridge, plan = self._bridge_and_plan(lambda p: ('warn', 'FAIL: H1 空心片段'))
        ret = bridge(0, 'video_done', {'video_url': self._downloaded()})
        self.assertIsNone(ret)
        self.assertTrue(os.path.exists(plan['dest_path']))
        self.assertEqual(self.records[0]['status'], 'success')
        self.assertIn('H1', self.records[0]['process_check'])
        stages = [s for s, _ in self.events]
        self.assertIn('video_warning', stages)
        self.assertLess(stages.index('video_warning'), stages.index('video_done'))

    def test_accept_records_success_with_trace(self):
        bridge, plan = self._bridge_and_plan(lambda p: ('accept', 'PASS'))
        ret = bridge(0, 'video_done', {'video_url': self._downloaded()})
        self.assertIsNone(ret)
        self.assertEqual(self.records[0]['status'], 'success')
        self.assertEqual(self.records[0]['process_check'], 'PASS')
        self.assertNotIn('video_warning', [s for s, _ in self.events])

    def test_no_check_fn_keeps_old_behavior(self):
        bridge, plan = self._bridge_and_plan(None)
        ret = bridge(0, 'video_done', {'video_url': self._downloaded()})
        self.assertIsNone(ret)
        self.assertEqual(self.records[0]['status'], 'success')
        self.assertNotIn('process_check', self.records[0])


_HAS_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


@unittest.skipUnless(_HAS_FFMPEG, 'ffmpeg/ffprobe 不可用')
class TestExtractMidFramesRealFfmpeg(unittest.TestCase):
    """真实 ffmpeg 集成：对合成的 2 秒测试视频抽 25%/50%/75% 三帧。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, 'seg.mp4')
        subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'testsrc=duration=2:size=160x120:rate=12', self.video],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extracts_three_nonempty_frames(self):
        out = os.path.join(self.tmp, 'mids')
        os.makedirs(out)
        paths = _extract_video_mid_frames(self.video, out)
        self.assertEqual(len(paths), 3)
        for p in paths:
            self.assertTrue(os.path.exists(p))
            self.assertGreater(os.path.getsize(p), 0)

    def test_garbage_file_returns_empty(self):
        bad = os.path.join(self.tmp, 'bad.mp4')
        with open(bad, 'wb') as f:
            f.write(b'not a video')
        self.assertEqual(_extract_video_mid_frames(bad, self.tmp), [])


if __name__ == '__main__':
    unittest.main()
